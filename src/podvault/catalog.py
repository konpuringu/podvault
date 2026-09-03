"""Engine records stored at predictable Azure Blob paths."""

from typing import Any, Dict, List, Optional

from .azure import AzureBlobClient
from .errors import ConfigurationError
from .receipts import utc_now


CATALOG_PREFIX = ".podvault/projects"
SUPPORTED_ENGINES = ("kopia", "azcopy")


def project_record_path(project: str) -> str:
    return "{}/{}/project.json".format(CATALOG_PREFIX, project)


class ProjectCatalog:
    def __init__(self, blobs: AzureBlobClient):
        self.blobs = blobs

    def get(self, project: str) -> Optional[Dict[str, Any]]:
        value = self.blobs.get_json(project_record_path(project), missing_ok=True)
        if value is None:
            return None
        if value.get("schema_version") != 1 or value.get("project") != project:
            raise ConfigurationError("invalid remote project record for {}".format(project))
        engine = value.get("engine")
        if engine not in SUPPORTED_ENGINES:
            raise ConfigurationError(
                "remote project {} uses unsupported engine: {}".format(project, engine)
            )
        return value

    def commit(
        self,
        project: str,
        engine: str,
        current_snapshot: Optional[str] = None,
    ) -> Dict[str, Any]:
        if engine not in SUPPORTED_ENGINES:
            raise ConfigurationError("unsupported project engine: {}".format(engine))
        previous = self.get(project)
        if previous and previous.get("engine") != engine:
            raise ConfigurationError(
                "project {} is already stored with the {} engine".format(
                    project, previous.get("engine")
                )
            )
        value: Dict[str, Any] = {
            "schema_version": 1,
            "project": project,
            "engine": engine,
            "created_at": previous.get("created_at") if previous else utc_now(),
            "updated_at": utc_now(),
        }
        if current_snapshot:
            value["current_snapshot"] = current_snapshot
        elif previous and previous.get("current_snapshot"):
            value["current_snapshot"] = previous["current_snapshot"]
        self.blobs.put_json(project_record_path(project), value)
        return value

    def list_records(self) -> List[Dict[str, Any]]:
        suffix = "/project.json"
        result = []
        for item in self.blobs.list_blobs(CATALOG_PREFIX + "/"):
            name = str(item.get("name") or "")
            if not name.endswith(suffix):
                continue
            value = self.blobs.get_json(name)
            if value and value.get("engine") in SUPPORTED_ENGINES:
                result.append(value)
        return result
