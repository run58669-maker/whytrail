#!/bin/sh
# Step 2: keep only rows with a plausible pickup date. The yellow_taxi sample carries a few trips
# whose timestamps fall outside the sample's real range; the daily aggregate inherits one junk row
# per such date. Run inside the driver container:
#
#   docker exec zetaris-driver-1 sh -s < scripts/zetaris/02_clean_taxi_daily.sh
#
# Output: /home/zetaris/customer-data/shared/whytrail/taxi_daily_clean/taxi_daily.csv
# (header + 2,002 rows, 2024-04-01..2025-02-28, 6 boroughs, 38,539,715 trips in total).
set -e
cd /home/zetaris/customer-data/shared/whytrail
mkdir -p taxi_daily_clean
{
  echo trip_date,borough,trips,fare_total_usd
  cat taxi_daily/part-*.csv | grep -v '^trip_date' | awk -F, '$1>="2024-04-01" && $1<="2025-02-28"' | sort
} > taxi_daily_clean/taxi_daily.csv
wc -l taxi_daily_clean/taxi_daily.csv
