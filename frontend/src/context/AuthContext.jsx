import { createContext, useContext, useEffect, useState } from "react";
import { onAuthStateChanged, signInWithPopup, signOut } from "firebase/auth";
import { auth, googleProvider } from "../firebase";

const AuthContext = createContext(null);

export function AuthProvider({ children }) {
  // undefined = still loading, null = signed out, User object = signed in
  // When Firebase is not configured, skip to null immediately (local dev mode).
  const [user, setUser] = useState(auth ? undefined : null);

  useEffect(() => {
    if (!auth) return;
    const unsubscribe = onAuthStateChanged(auth, setUser);
    return unsubscribe;
  }, []);

  const signInWithGoogle = async () => {
    try {
      await signInWithPopup(auth, googleProvider);
    } catch (err) {
      if (err.code !== "auth/popup-closed-by-user") throw err;
    }
  };

  const logout = () => signOut(auth);

  // Returns a fresh-enough ID token for auth headers / WS token.
  // Returns null when Firebase is not configured (local dev).
  const getToken = user ? () => user.getIdToken() : null;

  return (
    <AuthContext.Provider value={{ user, signInWithGoogle, logout, getToken }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth() {
  return useContext(AuthContext);
}
