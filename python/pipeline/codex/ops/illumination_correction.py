import os.path as osp
import numpy as np
import pandas as pd
from collections import OrderedDict
from codex.ops import op as codex_op
from codex import io as codex_io
from codex.cytometry.cytometer import DEFAULT_CHANNEL_PREFIX
from codex.function import data as function_data
from sklearn.ensemble import GradientBoostingRegressor
import logging
logger = logging.getLogger(__name__)

DEFAULT_FILTER_RANGE = [.1, .9]
DEFAULT_FILTER_FEATURES = ['cell_diameter']
SEED = 5512


class IlluminationCorrection(codex_op.CodexOp):

    def __init__(self, config, max_cells=100000, n_estimators=25,
                 filter_range=DEFAULT_FILTER_RANGE, filter_features=DEFAULT_FILTER_FEATURES):
        super().__init__(config)

        params = config.illumination_correction_params

        # Get mapping of source to target channel names, and sort by source channel
        self.channel_mapping = pd.Series(params['channel_mapping']).sort_index()

        self.filter_range = params.get('filter_range', filter_range)
        self.max_cells = params.get('max_cells', max_cells)
        self.n_estimators = params.get('n_estimators', n_estimators)
        self.filter_features = params.get('filter_features', filter_features)

        if self.filter_range is None or len(self.filter_range) != 2:
            raise ValueError(
                'Must provide filter range as 2 item list (given = {})'
                .format(self.filter_range)
            )
        for v in self.filter_range:
            if not 0 <= v <= 1:
                raise ValueError(
                    'Filter range percentile values must be in [0, 1] (given = {})'
                    .format(self.filter_range)
                )

        self.data = None
        self.data_saved = False

    def get_filter_masks(self, df, features):
        """Get masks for each filtered feature

        Args:
            df: Cytometry dataframe
            features: List of features to perform percentile filtering on
        Returns:
            DataFrame with column for each filter feature and values equal to boolean mask
                (where true means that the record is WITHIN the desired range)
        """
        # Compute low/high thresholds as dataframe like:
        #     feat_1 feat_2
        # p_lo    .1     10
        # p_hi   5.3    100
        ranges = df[features].quantile(q=self.filter_range)

        # Stack masks horizontally as dataframe
        return pd.concat([
            df[c].between(ranges[c].iloc[0], ranges[c].iloc[1])
            for c in ranges
        ], axis=1)

    def prepare_region_data(self, output_dir):
        if self.data is not None:
            return
        # Use whatever cytometry data was generated, whether it was for best
        # z planes, all planes, or a specific one
        df = function_data.get_cytometry_data(output_dir, self.config, mode='all')
        n = len(df)
        if df is None or n == 0:
            raise ValueError('Cytometry data cannot be empty in order to use it for illumination correction')

        self.data = {}
        for region_index, region_data in df.groupby('region_index'):
            ests = self.get_illumination_models(region_index, region_data)
            imgs = self.get_illumination_images(ests)
            self.data[region_index] = (imgs, ests)

    def get_illumination_models(self, region_index, df):
        n = len(df)
        ests = OrderedDict()
        for channel in self.channel_mapping.index:

            # Set list of features to filter on
            feature = DEFAULT_CHANNEL_PREFIX + channel
            filter_features = [feature] + self.filter_features

            # Restrict cell data to only records matching the given filters
            dfm = df[self.get_filter_masks(df, filter_features).all(axis=1).values].copy()

            # If necessary, downsample modeling data to improve performance
            if len(dfm) > self.max_cells:
                dfm = dfm.sample(n=self.max_cells, random_state=SEED)

            if len(dfm) == 0:
                raise ValueError(
                    'Cytometry data empty after application of feature filters for channel {}'.format(channel))

            logger.debug(
                'Building illumination model for region %s, channel "%s" using %s cells (%s originally)',
                region_index, channel, len(dfm), n
            )

            # Extract spatial cell features and prediction target
            X, y = dfm[['ry', 'rx']], dfm[feature]
            if np.isclose(y.mean(), 0):
                raise ValueError(
                    'Average {} channel intensity for region {} (across {} cells) is ~0, '
                    'making illumination correction impossible'
                    .format(region_index, channel, len(df))
                )
            y = y / y.mean()

            # Fit regression model used to represent illumination surface
            est = GradientBoostingRegressor(n_estimators=self.n_estimators)
            ests[channel] = est.fit(X, y)
        return ests

    def get_illumination_images(self, ests):
        """Get an illumination image by predicting the intensity at each pixel across a region

        Args:
            ests: Illumination models
        Returns:
            Dictionary mapping source channels to a 3D float32 array with shape equal to
            (region height * tile height, region width * tile width)
        """
        # Get whole region shape as rows, cols
        r, c = (
            self.config.region_height * self.config.tile_height,
            self.config.region_width * self.config.tile_width
        )
        ii = np.transpose([np.repeat(np.arange(r), c), np.tile(np.arange(c), r)])
        X = pd.DataFrame(ii, columns=['ry', 'rx'])

        imgs = OrderedDict()
        for channel, est in ests.items():
            img = est.predict(X).reshape((r, c)).astype(np.float32)
            imgs[channel] = img

        if len(imgs) > 0:
            img = list(imgs.values())[0]
            logger.debug('Resulting illumination image array shape = %s (dtype = %s)', img.shape, img.dtype)

        return imgs

    def save_region_data(self, output_dir):
        if self.data is None:
            raise ValueError('Region data cannot be saved until `prepare_region_data` is called')
        if self.data_saved:
            return None
        path = None
        for region_index, (imgs, ests) in self.data.items():
            # Stack 2D images on first axis to give 3D array
            img = np.stack(list(imgs.values()), 0)
            assert img.ndim == 3, 'Expecting 3D array, got shape {}'.format(img.shape)
            path = osp.join(output_dir, codex_io.get_illumination_image_path(region_index))
            codex_io.save_image(path, img)

        self.data_saved = True
        return osp.dirname(path or '')

    def _run(self, tile, tile_indices):
        # Get illumination image for this region
        imgs = self.data[tile_indices.region_index][0]

        # Get tile type information prior to multiplication
        dtype = tile.dtype
        dinfo = np.iinfo(dtype)

        # Determine starting offsets for tile slicing
        r, c = tile_indices.tile_y * self.config.tile_height, tile_indices.tile_x * self.config.tile_width

        # Loop through channel mapping and apply each adjustment
        tile = tile.astype(np.float32)
        for source_channel, target_channel in self.channel_mapping.to_dict().items():

            logger.debug('Applying correction from source channel "%s" to target "%s"', source_channel, target_channel)

            # Extract matching patch in illumination image
            img = imgs[source_channel][r:(r + self.config.tile_height), c:(c + self.config.tile_width)]

            # If application is across all channels, do single matrix division
            if target_channel == 'all':
                # Divide correction across HW dimensions of tile (must first add leading dims to match 5D tile)
                tile = tile / img[np.newaxis, np.newaxis, np.newaxis, :, :]

            # Otherwise, find target channel's coordinates within
            # tile and apply adjustment only to that slice
            else:
                cyc, ch = self.config.get_channel_coordinates(target_channel)
                tile[cyc, :, ch, :, :] = tile[cyc, :, ch, :, :] / img[np.newaxis, :, :]

        # Clip to range of original data type and convert
        tile = tile.clip(dinfo.min, dinfo.max).astype(dtype)

        return tile

    def save(self, tile_indices, output_dir, tile):
        # Overwrite the original preprocessed tile
        path = codex_io.get_processor_img_path(tile_indices.region_index, tile_indices.tile_x, tile_indices.tile_y)
        codex_io.save_tile(osp.join(output_dir, path), tile)
        return path

