"""Ensure importing app modules at collection time never touches /data.

Individual tests still point SPARK_DATA_DIR at their own tmp_path (and reload
app.db) when they need a real database.
"""

import os
import tempfile

os.environ.setdefault("SPARK_DATA_DIR", os.path.join(tempfile.gettempdir(), "spark-test-data"))
