"""ATNM copy check: every table of each client (ATNM) database against its copy on RDS.

Kept apart from the rest of the app, which checks RDS -> AlTasnimBI. Only app.main
mounts the router (app.ATNM.api).
"""
