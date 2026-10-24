-- Step 1: materialise the daily x borough aggregate of the built-in NY_TAXI.yellow_taxi sample.
-- One statement, run through scripts/zetaris/zsql.py with a long timeout (it scans the whole
-- trip-level table: 1,943 s = 32 min on a laptop Docker instance with 8 GB / 4 CPUs):
--
--   python scripts/zetaris/zsql.py "$(cat scripts/zetaris/01_taxi_daily_export.sql)" 3600
--
-- optionsBase64 is base64 of {"header":"true"}. The CSV lands inside the driver container at
-- /home/zetaris/customer-data/shared/whytrail/taxi_daily/part-*.csv (the result row is
-- ['file:///home/zetaris/customer-data/shared/whytrail/taxi_daily', 'SUCCESS', None]).
RUN EXPORT TO STORAGE (
  query = 'SELECT substr(y.tpep_pickup_datetime,1,10) AS trip_date,
                  z.borough AS borough,
                  COUNT(*) AS trips,
                  ROUND(SUM(CAST(y.fare_amount AS DOUBLE)),2) AS fare_total_usd
           FROM NY_TAXI.yellow_taxi y
           JOIN (SELECT CAST(LocationID AS INT) AS lid, MIN(borough) AS borough
                 FROM NY_TAXI.taxi_zones GROUP BY CAST(LocationID AS INT)) z
             ON CAST(y.PULocationID AS INT) = z.lid
           GROUP BY substr(y.tpep_pickup_datetime,1,10), z.borough',
  path = 'file:///home/zetaris/customer-data/shared/whytrail/taxi_daily',
  format = 'csv',
  saveMode = 'overwrite',
  optionsBase64 = 'eyJoZWFkZXIiOiJ0cnVlIn0='
)
