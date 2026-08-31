<script setup lang="ts">
// v2 P5 step 7: the login wall. What an unauthenticated visitor sees, and the
// whole of it (decisions-p5.md; D8 rules out ceremony, and this is not a new
// product surface).
//
// Registration is CLOSED -- accounts are seeded by scripts/seed_account.py and
// there is no HTTP path that creates one. So there is deliberately no sign-up
// link, no password reset, and no "forgot?" affordance: every one of them
// would be a control pointing at an endpoint that does not exist.

import { ref } from "vue"
import { login, loginError, loginPending } from "../state/auth"

const username = ref("")
const password = ref("")

async function onSubmit(): Promise<void> {
  if (loginPending.value) return
  const ok = await login(username.value.trim(), password.value)
  // Clear the password either way. On success it has served its purpose; on
  // failure leaving it in the DOM buys nothing and the field is retyped anyway.
  password.value = ""
  if (ok) username.value = ""
}
</script>

<template>
  <main class="login">
    <form class="login__card" @submit.prevent="onSubmit">
      <h1 class="login__title">Mitya</h1>
      <p class="login__sub">Sign in to read.</p>

      <label class="login__label" for="login-username">Account</label>
      <input
        id="login-username"
        v-model="username"
        class="login__input"
        type="text"
        autocomplete="username"
        autocapitalize="none"
        spellcheck="false"
        required
      />

      <label class="login__label" for="login-password">Password</label>
      <input
        id="login-password"
        v-model="password"
        class="login__input"
        type="password"
        autocomplete="current-password"
        required
      />

      <!-- One message for every failure -- the server refuses to distinguish
           an unknown account from a wrong password (D7), and the UI must not
           reintroduce the distinction it went to the trouble of removing. -->
      <p v-if="loginError" class="login__error" role="alert">{{ loginError }}</p>

      <button class="login__submit" type="submit" :disabled="loginPending">
        {{ loginPending ? "Signing in…" : "Sign in" }}
      </button>
    </form>
  </main>
</template>

<style scoped>
.login {
  display: flex;
  align-items: center;
  justify-content: center;
  height: 100%;
  padding: var(--sp-6);
}

.login__card {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
  width: 100%;
  max-width: 22rem;
  padding: var(--sp-6);
  background: var(--panel);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-ambient);
}

.login__title {
  font-family: var(--font-reading);
  font-weight: 500;
  font-size: var(--fs-display);
  color: var(--ink);
}

.login__sub {
  margin-bottom: var(--sp-4);
  font-family: var(--font-chrome);
  font-size: var(--fs-label);
  color: var(--ink-muted);
}

.login__label {
  font-family: var(--font-chrome);
  font-size: var(--fs-label);
  color: var(--ink-muted);
}

.login__input {
  padding: var(--sp-2) var(--sp-3);
  font-family: var(--font-chrome);
  font-size: var(--fs-body);
  color: var(--ink);
  background: var(--panel-2);
  border: none;
  border-radius: var(--radius-md);
}

.login__input:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}

.login__error {
  font-family: var(--font-chrome);
  font-size: var(--fs-label);
  color: var(--accent);
}

.login__submit {
  margin-top: var(--sp-4);
  padding: var(--sp-3);
  font-family: var(--font-chrome);
  font-size: var(--fs-label);
  color: var(--on-accent);
  background: var(--accent);
  border: none;
  border-radius: var(--radius-md);
  cursor: pointer;
}

.login__submit:hover:not(:disabled) {
  background: var(--accent-hover);
}

.login__submit:disabled {
  background: var(--accent-muted);
  cursor: default;
}
</style>
