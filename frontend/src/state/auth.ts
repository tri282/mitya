// v2 P5 step 7: the login wall's state (decisions-p5.md, D7/D8).
//
// Deliberately the smallest thing that works. The session lives in an
// HttpOnly cookie the browser sets and sends on its own -- JS cannot read it
// and must not try, which is the entire point of HttpOnly. So this module
// holds no token and no expiry timer. It holds one question -- "does the
// server currently accept me?" -- answered only by the server, and refreshed
// on exactly two events: page load, and any 401 the app sees.
//
// That second event is what covers session expiry INCLUDING mid-stream: the
// SSE connection is a normal fetch, an expired session makes /chat answer 401,
// and useChat routes that here. There is no clock, no countdown, and nothing
// to drift out of step with the server's own TTL.
//
// Module-level state, not a per-caller composable instance -- the same reason
// useHealth() keeps its status at module scope: two independent copies of
// "am I logged in" is exactly the bug that renders the app behind a login
// wall that has already been passed.

import { ref } from "vue"
import { getSession, postLogin, postLogout } from "../api/client"

/** null = not authenticated. The tenant id doubles as the display name; the
 *  server has no other notion of a user. */
export const tenant = ref<string | null>(null)
/** Until the first /auth/session answers we know nothing -- rendering the
 *  login wall during that window would flash it at an authenticated user on
 *  every reload. */
export const checked = ref(false)
export const loginError = ref<string | null>(null)
export const loginPending = ref(false)

export async function refresh(): Promise<void> {
  tenant.value = await getSession()
  checked.value = true
}

export async function login(username: string, password: string): Promise<boolean> {
  loginPending.value = true
  loginError.value = null
  try {
    const who = await postLogin(username, password)
    tenant.value = who
    return true
  } catch (e) {
    // One message for every failure, matching the server's own generic
    // response (D7: no user enumeration). The client must not add a
    // distinction the server deliberately refused to make -- a 429 is the one
    // exception, because telling someone to wait is not an existence oracle
    // and silently failing forever is worse.
    loginError.value =
      e instanceof Error && e.message === "429"
        ? "Too many attempts. Wait a few minutes and try again."
        : "Those credentials were not accepted."
    return false
  } finally {
    loginPending.value = false
  }
}

export async function logout(): Promise<void> {
  await postLogout()
  tenant.value = null
}

/** Called from anywhere that sees a 401 -- notably useChat, including in the
 *  middle of a stream. Drops to the login wall without reloading the page, so
 *  the transcript is still there afterwards. */
export function onUnauthenticated(): void {
  tenant.value = null
  checked.value = true
}
