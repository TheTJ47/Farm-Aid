import ee
import yaml

with open("config.yaml", encoding="utf-8") as f:
    config = yaml.safe_load(f)

project_id = config["gee_project"]

ee.Initialize(project=project_id)

result = ee.String("Earth Engine connected").getInfo()
print(result)

collection = (
    ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
    .filterBounds(ee.Geometry.Point([74.65, 19.33]))
    .filterDate("2025-10-01", "2025-11-01")
)

print("Sentinel-2 scenes:", collection.size().getInfo())