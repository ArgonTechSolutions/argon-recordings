// Browser binary/JSON transport only; all credential verification is server-side.
import { request } from "./api"

const decode = (value: string): ArrayBuffer => {
  const raw = atob(value.replace(/-/g, "+").replace(/_/g, "/"))
  return Uint8Array.from(raw, (char) => char.charCodeAt(0)).buffer
}
const encode = (value: ArrayBuffer) =>
  btoa(String.fromCharCode(...new Uint8Array(value)))
    .replace(/\+/g, "-")
    .replace(/\//g, "_")
    .replace(/=/g, "")
type Descriptor = { id: string; type: PublicKeyCredentialType }

export async function performPasskey(
  register: boolean,
  code = "",
  name = "Passkey"
) {
  if (!window.isSecureContext || !window.PublicKeyCredential)
    throw new Error(
      "Passkeys require HTTPS or a localhost SSH tunnel and a supported browser."
    )
  const kind = register ? "register" : "authenticate"
  const options = await request<Record<string, unknown>>(
    `/api/auth/mfa/${kind}/options`,
    {
      method: "POST",
      ...(register ? { body: JSON.stringify({ code }) } : {}),
    }
  )
  const publicKey = {
    ...options,
    challenge: decode(options.challenge as string),
  }
  let credential: PublicKeyCredential | null
  if (register) {
    const user = options.user as {
      id: string
      name: string
      displayName: string
    }
    credential = (await navigator.credentials.create({
      publicKey: {
        ...publicKey,
        user: { ...user, id: decode(user.id) },
        excludeCredentials: (options.excludeCredentials as Descriptor[]).map(
          (item) => ({ ...item, id: decode(item.id) })
        ),
      } as PublicKeyCredentialCreationOptions,
    })) as PublicKeyCredential | null
  } else {
    credential = (await navigator.credentials.get({
      publicKey: {
        ...publicKey,
        allowCredentials: (options.allowCredentials as Descriptor[]).map(
          (item) => ({ ...item, id: decode(item.id) })
        ),
      } as PublicKeyCredentialRequestOptions,
    })) as PublicKeyCredential | null
  }
  if (!credential) throw new Error("Passkey request was cancelled. Try again.")
  const response = credential.response
  const json = {
    id: credential.id,
    rawId: encode(credential.rawId),
    type: credential.type,
    response:
      response instanceof AuthenticatorAttestationResponse
        ? {
            clientDataJSON: encode(response.clientDataJSON),
            attestationObject: encode(response.attestationObject),
            transports: response.getTransports(),
          }
        : {
            clientDataJSON: encode(response.clientDataJSON),
            authenticatorData: encode(
              (response as AuthenticatorAssertionResponse).authenticatorData
            ),
            signature: encode(
              (response as AuthenticatorAssertionResponse).signature
            ),
            userHandle: (response as AuthenticatorAssertionResponse).userHandle
              ? encode((response as AuthenticatorAssertionResponse).userHandle!)
              : null,
          },
    clientExtensionResults: credential.getClientExtensionResults(),
  }
  return request<{ recovery_codes?: string[] }>(
    `/api/auth/mfa/${kind}/verify`,
    {
      method: "POST",
      body: JSON.stringify({ credential: json, name }),
    }
  )
}
