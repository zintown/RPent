# Copyright 2026 The RPent Authors.
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

"""Compatibility entry point for the Unitree task runner."""

from robots.unitree.mcp.task.runner import (
    ACTIVE_STATUSES as ACTIVE_STATUSES,
)
from robots.unitree.mcp.task.runner import (
    RunRequest as RunRequest,
)
from robots.unitree.mcp.task.runner import (
    TaskConflict as TaskConflict,
)
from robots.unitree.mcp.task.runner import (
    TaskRunner as TaskRunner,
)
from robots.unitree.mcp.task.runner import (
    validate_task_id as validate_task_id,
)
from robots.unitree.mcp.task.server import create_app as create_app
from robots.unitree.mcp.task.server import main as main

if __name__ == "__main__":
    main()
