The Stage 12 deployment check now uses one database connection for its role check, schema reads and rolled-back test writes, so it needs one free client slot on the database pooler.
