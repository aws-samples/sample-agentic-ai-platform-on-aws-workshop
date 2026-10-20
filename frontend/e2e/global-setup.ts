import { execFileSync } from "node:child_process";

/**
 * Persona passwords for the Hosted UI sign-in. Environment variables win; otherwise the users secret of the Auth stack is
 * read with the AWS CLI (AWS_PROFILE defaults to `workshop`), the same source `scripts/doctor.py --show-passwords` prints.
 */
const PERSONAS = ["admin", "developer", "approver"] as const;

function envName(persona: string): string {
  return `E2E_${persona.toUpperCase()}_PASSWORD`;
}

export default function globalSetup(): void {
  const missing = PERSONAS.filter((p) => !process.env[envName(p)]);
  if (!missing.length) {
    return;
  }
  const env = { ...process.env, AWS_PROFILE: process.env.AWS_PROFILE ?? "workshop", AWS_DEFAULT_REGION: process.env.AWS_DEFAULT_REGION ?? "us-east-1" };
  const run = (args: string[]) => execFileSync("aws", args, { env, encoding: "utf8" }).trim();
  const secretArn = run(["ssm", "get-parameter", "--name", "/agent-platform/auth/users-secret-arn", "--query", "Parameter.Value", "--output", "text"]);
  const secret = JSON.parse(run(["secretsmanager", "get-secret-value", "--secret-id", secretArn, "--query", "SecretString", "--output", "text"])) as Record<
    string,
    { username: string; password: string }
  >;
  for (const persona of missing) {
    const user = secret[persona];
    if (!user?.password) {
      throw new Error(`users secret has no password for ${persona}`);
    }
    process.env[envName(persona)] = user.password;
    process.env[`E2E_${persona.toUpperCase()}_USERNAME`] = user.username;
  }
  console.log(`[e2e] persona passwords loaded from the users secret for ${missing.join(", ")}`);
}
