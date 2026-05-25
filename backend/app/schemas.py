from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class IngestRequest(BaseModel):
    url: str = Field(
        ...,
        description="Public GitHub repo URL, e.g. https://github.com/octocat/Hello-World",
    )


class FileOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    path: str
    size_bytes: int
    language: str | None


class RepoOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    owner: str
    name: str
    default_branch: str
    ingested_at: datetime


class RepoFilesResponse(BaseModel):
    repo: RepoOut
    files: list[FileOut]
    file_count: int
