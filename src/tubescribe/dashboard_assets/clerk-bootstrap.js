"use strict";

window.tubeScribeClerkReady ??= (async () => {
  const bootstrap = document.querySelector('meta[name="tubescribe-config"]');
  let settings;
  if (bootstrap) {
    settings = JSON.parse(bootstrap.content);
  } else {
    const response = await fetch("/auth/config", {cache: "no-store"});
    if (!response.ok) throw new Error("Sign-in configuration is unavailable. Refresh to retry.");
    settings = await response.json();
  }
  const frontend = new URL(settings.frontend_api);
  if (frontend.protocol !== "https:" || frontend.pathname !== "/") throw new Error("Invalid sign-in configuration");

  // Wait for the parser, without waiting for the dashboard's deferred script.
  if (document.readyState === "loading") {
    await new Promise(resolve => document.addEventListener("readystatechange", resolve, {once: true}));
  }
  const [ui, sdk] = settings.browser_scripts.map(path => frontend.origin + path);
  for (const url of [settings.ui_script ?? ui, sdk]) {
    await new Promise((resolve, reject) => {
      const script = document.createElement("script");
      script.src = url;
      script.crossOrigin = "anonymous";
      script.dataset.clerkPublishableKey = settings.publishable_key;
      script.onload = resolve;
      script.onerror = () => reject(new Error("Sign-in could not load. Refresh to retry."));
      document.head.append(script);
    });
  }
  await window.Clerk.load({
    ui: {ClerkUI: window.__internal_ClerkUICtor},
    localization: {signIn: {start: {title: "Sign in to TubeScribe", subtitle: "Your videos, ready to read."}}},
    appearance: {
      variables: {colorPrimary: "#21644e", colorForeground: "#141b29", colorMutedForeground: "#657084", fontFamily: "inherit", borderRadius: "8px"},
      elements: {
        rootBox: "auth-root", cardBox: "auth-card-box", card: "auth-card",
        headerTitle: "auth-title", headerSubtitle: "auth-subtitle",
        socialButtonsBlockButton: "auth-social", formFieldInput: "auth-input",
        formButtonPrimary: "auth-submit", footer: "auth-footer",
      },
    },
  });

  const container = document.getElementById("sign-in");
  let mounted = false;
  function setSignInVisible(visible) {
    if (visible === mounted) return;
    if (visible) {
      window.Clerk.mountSignIn(container, {routing: "hash", forceRedirectUrl: "/" + window.location.search});
    } else {
      window.Clerk.unmountSignIn(container);
    }
    mounted = visible;
  }

  // Sign-in can start loading its UI before the dashboard script is available.
  if (!window.Clerk.user || !window.Clerk.session) {
    document.getElementById("auth-message").textContent = "";
    setSignInVisible(true);
  }
  return setSignInVisible;
})();
// The reader handles any error when it awaits startup, even if it arrives later.
window.tubeScribeClerkReady.catch(() => {});
