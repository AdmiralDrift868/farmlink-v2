# backup.sh
#!/bin/bash
DATE=$(date +%Y%m%d)
pg_dump -U farmlink_user farmlink_prod > /backups/farmlink_$DATE.sql
find /backups -type f -mtime +30 -delete