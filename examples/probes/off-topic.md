Move the job store from SQLite to PostgreSQL before deciding anything else.

SQLite takes a single write lock, which becomes the bottleneck once the Mac and the desktop both submit results at the same time. PostgreSQL gives concurrent writes, point-in-time recovery and a network protocol the Mac can use directly, so the worker no longer depends on the coordinator's file system.

Failure handling: if the database is unreachable, workers buffer results locally and retry with backoff. A test kills the database mid-write and checks that no result is lost or duplicated.

I have not measured SQLite lock contention yet. If a load test with both machines submitting shows under 5% of time spent waiting on the lock, keep SQLite instead.
