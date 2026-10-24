# Contributing

Use Python 3.12+, uv, Git and Git LFS:

```sh
git clone https://github.com/benjamin-di/wiki-hub-server
cd wiki-hub-server
make check
```

Tests run against synthetic data and fake providers. Do not use a real identity,
wiki, Git credential or model API to run the test suite. Keep bug reports and
fixtures free of personal data. Reproduce permission, recovery and publication
failures with focused tests; retain existing access and source-preservation
invariants. Report vulnerabilities using [the security policy](SECURITY.md).

## How changes land

Each public release is exported from a private working repository as one
`Release X.Y.Z` commit. Pull requests are reviewed here and applied in the source
repository, rather than merged into this mirror, because the next export would
replace direct edits. Accepted contributions are credited in release notes.
