"""Ground-surface estimate from the Copernicus GLO-30 surface model (one-off).

Copernicus is a surface model: roofs and trees lift city cells by 1-5 m. Bangkok is almost flat, so a low percentile
over a ~840 m window approximates the ground between buildings; then light smoothing.
in : data/dem_raw.npy (cm, 4x4-block 25th percentile of 30 m cells, fetched in the browser from AWS open data)
out: data/dem_ground.npy (cm), data/dem.json (meta + base64 int16 for the pages)
Heights are EGM2008 geoid metres (~ mean sea level, close to ม.รทก. but not identical); vertical error ~1 m.
"""
import json, os, base64
import numpy as np
from scipy import ndimage

HERE = os.path.dirname(os.path.abspath(__file__)); DATA = os.path.join(HERE, '..', 'data')
P = lambda f: os.path.join(DATA, f)
meta = json.load(open(P('dem_meta.json')))
z = np.load(P('dem_raw.npy')).astype(float)
water = z <= 1                                            # sea / river cells are flattened to 0 in the product
zz = np.where(water, 9999, z)
g = ndimage.percentile_filter(zz, 15, size=7)
g = np.where(g > 9000, np.nan, g)
# fill remaining gaps (wide water) from nearest valid value
idx = ndimage.distance_transform_edt(np.isnan(g), return_distances=False, return_indices=True)
g = g[tuple(idx)]
g = ndimage.gaussian_filter(g, 1.2)
g = np.where(water, np.minimum(g, 0), g)                  # keep the river/sea low
np.save(P('dem_ground.npy'), g.astype(np.float32))
q = np.round(g).astype('<i2')
meta.update(kind='ground estimate (15th percentile in 840 m window, smoothed)', b64=base64.b64encode(q.tobytes()).decode(),
            water=base64.b64encode(np.packbits(water.astype(np.uint8)).tobytes()).decode())
json.dump(meta, open(P('dem.json'), 'w'))
print('ground cm percentiles', np.percentile(g[~water], [5, 25, 50, 75, 95]).round(0), 'json KB', os.path.getsize(P('dem.json')) // 1024)
