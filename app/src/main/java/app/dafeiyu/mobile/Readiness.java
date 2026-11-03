package app.dafeiyu.mobile;

/** A readiness decision is evidence-based; the UI cannot start a missing engine. */
public final class Readiness {
    public enum State { ENGINE_MISSING, PHONE_UNVERIFIED, LOGIN_REQUIRED, USER_ACTION_REQUIRED, READY }

    private Readiness() {}

    public static State evaluate(boolean enginePresent, boolean phoneVerified,
                                 boolean authenticated, boolean userActionRequired) {
        if (userActionRequired) return State.USER_ACTION_REQUIRED;
        if (!enginePresent) return State.ENGINE_MISSING;
        if (!phoneVerified) return State.PHONE_UNVERIFIED;
        if (!authenticated) return State.LOGIN_REQUIRED;
        return State.READY;
    }
}
