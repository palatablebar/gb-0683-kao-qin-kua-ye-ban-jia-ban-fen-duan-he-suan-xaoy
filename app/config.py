TITLE='考勤跨夜班与加班分段核算'
SHORT='shiftledger'
RULE_VERSION='2026.11.17-v1'
MODULES=[('m1_ingest', '打卡采信'), ('m2_mapping', '班次日历'), ('m3_versions', '跨夜配对'), ('m4_segments', '缺卡复核'), ('m5_calculate', '工时切段'), ('m6_reconcile', '薪资对账'), ('m7_archive', '周期归档')]
REQUIRED=['id', 'employee', 'shift_day', 'version', 'punch', 'at', 'shift_start', 'shift_end', 'break_start', 'break_end', 'hourly_rate', 'paid']
NUMERIC=['version', 'hourly_rate', 'paid']
DATES=['shift_day']
TIMES=['at']
