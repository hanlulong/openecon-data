# Catalog Update Cron Job Setup

This guide explains how to set up automatic daily updates for the OpenEcon catalog database.

## Overview

The catalog database should be updated daily to ensure indicators remain current. The update process re-indexes all provider catalogs and typically takes 1-2 minutes.

## Setup Instructions

### 1. Create Log Directory

```bash
sudo mkdir -p /var/log/openecon
sudo chown $USER:$USER /var/log/openecon
```

### 2. Make Update Script Executable

```bash
cd /home/hanlulong/OpenEcon
chmod +x backend/data/update_catalog.py
```

### 3. Test the Update Script

Run the update script manually to ensure it works:

```bash
cd /home/hanlulong/OpenEcon
source backend/.venv/bin/activate
python backend/data/update_catalog.py
```

You should see output like:

```
======================================================================
OPENECON CATALOG UPDATE - 2025-11-02T02:00:00
======================================================================

Starting catalog update...

======================================================================
UPDATE RESULTS
======================================================================

✅ FRED: Indexed 5,821 indicators
✅ WORLDBANK: Indexed 29,255 indicators
✅ STATSCAN: Indexed 7,998 indicators
...

======================================================================
SUMMARY
======================================================================

Duration: 65.32 seconds
Successful providers: 9
Failed providers: 0
Total indicators: 73,106
```

### 4. Set Up Cron Job

Open your crontab:

```bash
crontab -e
```

Add this line to run the update daily at 2 AM:

```cron
# OpenEcon Catalog Update - Daily at 2 AM
0 2 * * * cd /home/hanlulong/OpenEcon && source backend/.venv/bin/activate && python backend/data/update_catalog.py >> /var/log/openecon/catalog_update.log 2>&1
```

Save and exit.

### 5. Verify Cron Job

Check that the cron job was added:

```bash
crontab -l
```

### 6. Monitor Logs

View the update logs:

```bash
tail -f /var/log/openecon/catalog_update.log
```

View recent updates:

```bash
tail -100 /var/log/openecon/catalog_update.log
```

## Alternative Schedule

If you want to run updates at a different time, modify the cron schedule:

```cron
# Daily at 3 AM
0 3 * * * cd /home/hanlulong/OpenEcon && ...

# Every 6 hours
0 */6 * * * cd /home/hanlulong/OpenEcon && ...

# Weekly on Sunday at 2 AM
0 2 * * 0 cd /home/hanlulong/OpenEcon && ...
```

## Log Rotation

To prevent log files from growing too large, set up log rotation.

Create `/etc/logrotate.d/openecon`:

```bash
sudo nano /etc/logrotate.d/openecon
```

Add:

```
/var/log/openecon/*.log {
    daily
    rotate 30
    compress
    delaycompress
    missingok
    notifempty
    create 644 hanlulong hanlulong
}
```

Test log rotation:

```bash
sudo logrotate -f /etc/logrotate.d/openecon
```

## Monitoring

### Check Last Update Time

```bash
sqlite3 backend/data/catalog.db "SELECT provider, last_indexed, status FROM index_metadata ORDER BY last_indexed DESC"
```

### Check Update Success

```bash
grep -E "(✅|❌|SUMMARY)" /var/log/openecon/catalog_update.log | tail -50
```

### Email Notifications

To receive email notifications on failures, install mailutils:

```bash
sudo apt-get install mailutils
```

Then modify the cron line to redirect only errors:

```cron
0 2 * * * cd /home/hanlulong/OpenEcon && source backend/.venv/bin/activate && python backend/data/update_catalog.py >> /var/log/openecon/catalog_update.log 2>&1 || echo "OpenEcon catalog update failed" | mail -s "OpenEcon Update Failed" your@email.com
```

## Troubleshooting

### Update Failed

1. Check the logs:
   ```bash
   tail -100 /var/log/openecon/catalog_update.log
   ```

2. Run the update manually to see full error output:
   ```bash
   cd /home/hanlulong/OpenEcon
   source backend/.venv/bin/activate
   python backend/data/update_catalog.py
   ```

3. Check API keys are valid in `.env`:
   ```bash
   grep -E "(FRED_API_KEY|COMTRADE_API_KEY)" .env
   ```

### Cron Job Not Running

1. Check cron service is running:
   ```bash
   sudo systemctl status cron
   ```

2. Check system logs:
   ```bash
   sudo grep CRON /var/log/syslog | tail -20
   ```

3. Verify crontab syntax:
   ```bash
   crontab -l
   ```

### Database Locked Error

If you see "database is locked" errors, ensure:
1. No other processes are accessing the database
2. The backend server is not running during updates (or uses read-only mode)

Solution: Stop backend during updates:

```cron
0 2 * * * cd /home/hanlulong/OpenEcon && sudo systemctl stop openecon-backend && source backend/.venv/bin/activate && python backend/data/update_catalog.py >> /var/log/openecon/catalog_update.log 2>&1 && sudo systemctl start openecon-backend
```

## Manual Update

To manually trigger an update at any time:

```bash
cd /home/hanlulong/OpenEcon
source backend/.venv/bin/activate
python backend/data/update_catalog.py
```

## Performance Notes

- Full update typically takes 1-2 minutes
- Database size: ~15-30 MB
- Network usage: ~10-50 MB (depends on provider APIs)
- CPU usage: Low (mostly I/O bound)
- Memory usage: ~100-200 MB peak
