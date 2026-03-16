import { createContext, useContext, useEffect, useState } from "react";
import { onAuthStateChanged, signInWithPopup, signOut } from "firebase/auth";
import { auth, googleProvider } from "../firebase";

const AuthContext = createContext(null);

export function AuthProvider({ children }) {
  const [user, setUser] = useState(auth ? undefined : null);
  const [isGuest, setIsGuest] = useState(false);

  useEffect(() => {
    if (!auth) return;
    const unsubscribe = onAuthStateChanged(auth, (u) => {
      setUser(u);
      if (u) setIsGuest(false); // clear guest mode on real sign-in
    });
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

  const continueAsGuest = () => setIsGuest(true);

  const getToken = user ? () => user.getIdToken() : null;

  return (
    <AuthContext.Provider value={{ user, isGuest, signInWithGoogle, continueAsGuest, logout, getToken }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth() {
  return useContext(AuthContext);
}
