# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
import unittest.mock
import google.auth
from google.auth.credentials import Credentials
from dotenv import load_dotenv

# Load env variables from .env file first
load_dotenv()

# Set default env vars for tests if not already set in .env
os.environ.setdefault("GOOGLE_CLOUD_PROJECT", "ambient-expense-agent")
os.environ.setdefault("GOOGLE_CLOUD_LOCATION", "global")
os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "True")

# Dummy credentials class to bypass google-auth validation in tests
class DummyCredentials(Credentials):
    def __init__(self, token="dummy-token"):
        super().__init__()
        self.token = token

    def refresh(self, request):
        pass

# Mock google.auth.default during test collection/execution if real credentials aren't present
try:
    google.auth.default()
except Exception:
    dummy_credentials = DummyCredentials()
    mock_default = unittest.mock.MagicMock(return_value=(dummy_credentials, "ambient-expense-agent"))
    google.auth.default = mock_default
