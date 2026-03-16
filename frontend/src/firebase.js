import { initializeApp } from "firebase/app";
import { getAuth, GoogleAuthProvider } from "firebase/auth";

// Skip Firebase Auth in local dev
// Set VITE_USE_AUTH_LOCAL=true to test Google sign-in locally (add localhost to Firebase Console first).
const _configured =
  !!import.meta.env.VITE_FIREBASE_API_KEY &&
  (import.meta.env.VITE_USE_AUTH_LOCAL === "true" || !import.meta.env.DEV);

export const auth = _configured
  ? getAuth(
      initializeApp({
        apiKey:            import.meta.env.VITE_FIREBASE_API_KEY,
        authDomain:        import.meta.env.VITE_FIREBASE_AUTH_DOMAIN,
        projectId:         import.meta.env.VITE_FIREBASE_PROJECT_ID,
        storageBucket:     import.meta.env.VITE_FIREBASE_STORAGE_BUCKET,
        messagingSenderId: import.meta.env.VITE_FIREBASE_MESSAGING_SENDER_ID,
        appId:             import.meta.env.VITE_FIREBASE_APP_ID,
      })
    )
  : null;

export const googleProvider = _configured ? new GoogleAuthProvider() : null;
export const isAuthConfigured = _configured;
