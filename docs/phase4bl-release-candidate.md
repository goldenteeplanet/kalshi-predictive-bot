# Phase 4BL — Dry-Run Release Candidate Packaging

Phase 4BL creates a deterministic manifest for a local, undeployed, non-production release
candidate. It requires verifier, simulator, schema, documentation, test, build-identity, and SBOM
inputs. Each file is path-confined and SHA-256 protected.

The packager rejects duplicate or escaping paths, symlinks, missing files, production or service
path names, credential-like files, known production paths, service commands, API-key material,
write authorization, and exchange surfaces. The manifest and safety artifact grant no authority.
