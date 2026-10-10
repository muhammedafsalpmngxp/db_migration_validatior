"""Direct report: the client's original databases on the ATNM server (AppMasterDB,
AppMasterEngDB) checked straight against the new system (AlTasnimBI on RDS), mapping by
mapping, with the migration plan's own rules (one to one, union, merge, transform).

Separate from the migration report (app/report): its own run, files, folder and endpoints
(/api/direct-report). It shares only the run guard, so it never loads the servers at the
same time as another run.

Version 1 reads the catalogs only (table names, columns, types, row counts from
sys.partitions): which tables exist, the record counts by the plan's rule, and how the
columns pair up. Values are not compared yet.

    settings.py   folders, files, test mode
    check.py      plan table -> ATNM / AlTasnimBI table, and the grading of one mapping
    jobs.py       the background run, its log and the saved reports
    xlsx.py       the Excel workbook
    api.py        GET/POST /api/direct-report/*
"""
