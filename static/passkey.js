// Passkey (WebAuthn) sign-in, shared by the login page and the in-app
// "signed out" prompt. Resolves to:
//   {ok: true}                      signed in; the session cookie is set
//   {ok: false, reason, message}    reason: 'cancelled' | 'rejected' | 'rate' | 'error'
(function () {
  const dec = s => Uint8Array.from(atob(s.replace(/-/g, '+').replace(/_/g, '/')), c => c.charCodeAt(0));
  const enc = b => btoa(String.fromCharCode(...new Uint8Array(b))).replace(/[+]/g, '-').replace(/[/]/g, '_').replace(/=+$/, '');
  let inFlight = null;

  async function run() {
    let cred, challenge_id;
    try {
      const r = await fetch('/login/passkey/options', { method: 'POST' });
      if (!r.ok) return { ok: false, reason: 'error', message: 'Could not reach the bridge. Try again.' };
      const j = await r.json();
      challenge_id = j.challenge_id;
      const options = j.options;
      options.challenge = dec(options.challenge);
      (options.allowCredentials || []).forEach(c => { c.id = dec(c.id); });
      cred = await navigator.credentials.get({ publicKey: options });
    } catch (e) {
      // NotAllowedError covers both "you dismissed the prompt" and "the browser
      // wouldn't show one without a tap"; neither is worth an error message.
      if (e && (e.name === 'NotAllowedError' || e.name === 'AbortError')) return { ok: false, reason: 'cancelled', message: '' };
      return { ok: false, reason: 'error', message: 'Passkey sign-in failed. Try again, or use the password.' };
    }
    try {
      const body = { challenge_id, credential: {
        id: cred.id, rawId: enc(cred.rawId), type: cred.type,
        response: {
          authenticatorData: enc(cred.response.authenticatorData),
          clientDataJSON: enc(cred.response.clientDataJSON),
          signature: enc(cred.response.signature),
          userHandle: cred.response.userHandle ? enc(cred.response.userHandle) : null,
        } } };
      const v = await fetch('/login/passkey/verify', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
      if (v.ok) return { ok: true };
      if (v.status === 429) return { ok: false, reason: 'rate', message: 'Too many attempts. Try again in a few minutes.' };
      return { ok: false, reason: 'rejected', message: "That passkey isn't enrolled on this bridge. Use the password, then add it in Preferences." };
    } catch (e) {
      return { ok: false, reason: 'error', message: 'Could not reach the bridge. Try again.' };
    }
  }

  window.passkeySupported = () => !!(window.PublicKeyCredential && navigator.credentials);
  // One prompt at a time: a second call while one is open joins the first.
  window.passkeySignIn = () => {
    if (!inFlight) inFlight = run().finally(() => { inFlight = null; });
    return inFlight;
  };
})();
