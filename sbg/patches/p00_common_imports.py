from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import re
import shutil
import sys
import tempfile
import time
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple, Union

from .. import globals as _g
from ..errors import *
from ..ast import *
from ..parser import *
from ..runtime import *
from ..scratch import *
from ..compiler import *
from ..packages import *
from ..cli import *
from ..optimizer import *
from ..compiler import _input_block_refs
from ..globals import (
    ACTION_PROC_NAME,
    BACKDROP_SVG,
    BUILTIN_EXPR_NAMES,
    BUILTIN_NAMES,
    BUILTIN_STMT_NAMES,
    KEYWORDS,
    MULTI,
    SINGLE,
    TERMINAL_LIST_ID,
    TERMINAL_LIST_NAME,
    VERSION,
)

