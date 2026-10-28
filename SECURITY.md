# Security and data handling

The default demo, tests and CI need no credentials. No account-specific configuration, credential file, private account response, machine path or existing trading record is included in this repository.

## Keep local data local

- Enter optional Testnet credentials only through the local hidden prompts.
- Never put secrets in source, shell arguments, issues, screenshots or commits.
- `var/`, `reports/`, environments, editor settings, databases, logs and secret files are ignored.
- The source archive builder uses an explicit path list and refuses symbolic links; it never archives the whole directory.
- Public-file checks catch selected credential patterns and personal machine paths. They cannot certify that an arbitrary edited file contains no secret; review changes before publishing.

Ignore rules do not remove a secret that was committed earlier. If one is exposed, revoke it at the provider and address repository history before sharing again.

## Network boundaries

Offline demonstrations and automated checks do not access accounts. Network-enabled research commands require an explicit opt-in and do not implement production order submission. The optional `marvel_testnet` adapter supports a fixed virtual-asset host and needs an additional explicit opt-in for orders.

Some research modules support optional authenticated read-only metadata or non-accepting conversion-quote observations. They remain independent of the offline quick start and are not run by CI. The quote endpoint's provider permission may be broader than the client's behavior. No real credentials or account observations are distributed.

## Report a problem

Describe reproducible behavior using invented data. Do not attach credentials, signed URLs, private API responses, database files or account identifiers to a public report. Preserve uncertain execution records locally for investigation.
