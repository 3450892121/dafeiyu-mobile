import json
import os

try:
    with open(os.path.join(os.path.dirname(__file__), '..', 'release.json'), encoding='utf-8') as stream:
        _release = json.load(stream)
except (OSError, ValueError):
    _release = {}
VERSION = _release.get('versionName', 'development')
BUILD = _release.get('buildDate', 'local')
