from __future__ import annotations

import atexit
import shutil
from pathlib import Path
from tempfile import mkdtemp

from django.conf import settings


if not getattr(settings, "_ODDESY_TEST_MEDIA_ROOT_CONFIGURED", False):
    test_media_root = Path(mkdtemp(prefix="oddesyagent-test-media-"))
    settings.MEDIA_ROOT = test_media_root
    settings._ODDESY_TEST_MEDIA_ROOT_CONFIGURED = True
    atexit.register(shutil.rmtree, test_media_root, True)
