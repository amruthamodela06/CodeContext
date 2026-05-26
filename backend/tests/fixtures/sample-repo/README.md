# Sample Repo

Test fixture for CodeContext ingestion tests. See [ADR 0003](../../../../docs/decisions/0003-vendored-git-fixture.md).

Note: the `.git/` directory of this fixture is stored on disk as `dot-git/` so our
project's git tracks it as plain files rather than treating it as a nested submodule.
The `sample_repo` pytest fixture renames `dot-git/` → `.git/` into a tmpdir before tests
clone or walk it.
