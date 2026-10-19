# Credentials

Nothing in ffagent reads a credential directly; everything asks the secrets resolver by name. The
resolver tries the backends listed in `FFAGENT_SECRETS` in order and takes the first value found.

| Backend | Set with | Good for |
|---|---|---|
| `env` (default) | `.env` file or exported variables | zero setup, CI, tests |
| `keyring` | `ffagent secrets set NAME` | a laptop: macOS Keychain, Windows Credential Locker, Linux Secret Service |
| `ssm` | `ffagent secrets set NAME` or the AWS CLI | laptop, container and cloud alike; free on the standard tier |

Names: `ESPN_S2`, `ESPN_SWID`, `FANTASY_INFORMATION_SOURCE_URL`, `FANTASY_INFORMATION_SOURCE_API_KEY`,
and the LLM provider key (`ANTHROPIC_API_KEY` by default). `ffagent secrets check` shows where each
one was found; `--probe` also tests the ESPN cookies against one of your leagues.

```bash
export FFAGENT_SECRETS=ssm,keyring,env     # try SSM, then the keyring, then .env
ffagent secrets check --probe
ffagent secrets set ESPN_S2                # prompts without echo; writes to the first writable backend
```

## AWS SSM Parameter Store setup (free, standard tier)

Parameters live under `/ffagent/` as SecureStrings encrypted with the AWS-managed `aws/ssm` key:
no parameter charge, no key charge, and KMS request costs at this volume round to zero.

1. Create the parameters (once per secret; `--overwrite` to rotate):

```bash
aws ssm put-parameter --name /ffagent/ESPN_S2 --type SecureString --value "$(read -s -p 'espn_s2: ' v; echo "$v")"
```

```bash
aws ssm put-parameter --name /ffagent/ESPN_SWID --type SecureString --value "$(read -s -p 'SWID: ' v; echo "$v")"
```

```bash
aws ssm put-parameter --name /ffagent/FANTASY_INFORMATION_SOURCE_API_KEY --type SecureString --value "$(read -s -p 'key: ' v; echo "$v")"
```

```bash
aws ssm put-parameter --name /ffagent/ANTHROPIC_API_KEY --type SecureString --value "$(read -s -p 'key: ' v; echo "$v")"
```

2. A read-only identity for the agent. Attach this policy to an IAM user (laptop, with its own
   access key in `~/.aws/credentials`) or a role (container, scheduler). Replace the region and
   account id.

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {"Effect": "Allow", "Action": ["ssm:GetParameter", "ssm:GetParameters"],
     "Resource": "arn:aws:ssm:us-east-1:123456789012:parameter/ffagent/*"},
    {"Effect": "Allow", "Action": "kms:Decrypt",
     "Resource": "arn:aws:kms:us-east-1:123456789012:key/*",
     "Condition": {"StringEquals": {"kms:ViaService": "ssm.us-east-1.amazonaws.com"}}}
  ]
}
```

3. Tell ffagent to use it:

```bash
export FFAGENT_SECRETS=ssm,env
export FFAGENT_SSM_REGION=us-east-1
ffagent secrets check --probe
```

The one credential left on the machine is the AWS access key, which can only read five parameters.

## Rotation

ESPN cookies expire every few weeks. `ffagent secrets check --probe` reports it; refresh with
`ffagent secrets set ESPN_S2` and `ffagent secrets set ESPN_SWID`, or `aws ssm put-parameter --overwrite`.
