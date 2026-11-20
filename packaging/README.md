# Candidate build scripts

See [installation and rollback](../docs/release/installation.md).
`desktop-packages.yml` builds unsigned candidate installers. It does not publish a release.
Checksums and selected runtime notices are generated beside outputs. Clean-machine
installation, complete component/license review, real devices and release approval
remain separate gates. Build output is not proof that those gates passed.

The protected native build and promotion workflows freeze public 0.4.0 packages. Each platform has its own immutable prerelease, followed by one stable release of the same three files. The Russian runbook includes signing-provider setup, offline bench receipts, CI provenance and promotion commands. [Public distribution](../docs/release/public-distribution.md)
