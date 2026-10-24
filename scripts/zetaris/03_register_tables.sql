-- Step 3: register the three tables WhyTrail queries. One statement per zsql.py call
-- (Lightning SQL accepts one statement per request), in this order.
--
-- Before this step the two reference CSV folders produced by prep_reference_tables.py must be inside
-- the driver container:
--   docker cp out/central_park_daily zetaris-driver-1:/home/zetaris/customer-data/shared/whytrail/
--   docker cp out/us_holidays        zetaris-driver-1:/home/zetaris/customer-data/shared/whytrail/
--
-- 3a. two new Lightning databases (the DESCRIBE BY text must not contain spaces or parentheses;
--     the first attempt with a free-text description was rejected)
CREATE LIGHTNING DATABASE NOAA_GHCN DESCRIBE BY 'NOAA_GHCN-Daily_Central_Park_USW00094728_for_WhyTrail';
CREATE LIGHTNING DATABASE REF DESCRIBE BY 'US_federal_holidays_for_WhyTrail';

-- 3b. reference tables as CSV file-store tables
CREATE LIGHTNING FILESTORE TABLE central_park_daily FROM NOAA_GHCN FORMAT CSV
  OPTIONS (path 'file:///home/zetaris/customer-data/shared/whytrail/central_park_daily', header 'true', inferSchema 'true', delimiter ',');
CREATE LIGHTNING FILESTORE TABLE us_holidays FROM REF FORMAT CSV
  OPTIONS (path 'file:///home/zetaris/customer-data/shared/whytrail/us_holidays', header 'true', delimiter ',');

-- 3c. the daily taxi aggregate, registered on the cleaned copy, inside the existing NY_TAXI database
--     (the built-in NY_TAXI tables are not modified)
CREATE LIGHTNING FILESTORE TABLE taxi_daily FROM NY_TAXI FORMAT CSV
  OPTIONS (path 'file:///home/zetaris/customer-data/shared/whytrail/taxi_daily_clean', header 'true', inferSchema 'true', delimiter ',');

-- 3d. readiness checks (expected on 2026-10-22: 402 | 2024-01-01 | 2025-02-05 | 351.0 ; 22 | 2024-01-01 | 2025-12-25 ;
--     2002 | 2024-04-01 | 2025-02-28 | 38539715). The first query on a fresh table takes 1-2 minutes
--     (schema inference); later template queries run in 0.4-2.3 s.
SELECT COUNT(*), MIN(obs_date), MAX(obs_date), SUM(snow_mm) FROM NOAA_GHCN.central_park_daily;
SELECT COUNT(*), MIN(holiday_date), MAX(holiday_date) FROM REF.us_holidays;
SELECT COUNT(*), MIN(trip_date), MAX(trip_date), SUM(trips) FROM NY_TAXI.taxi_daily;
