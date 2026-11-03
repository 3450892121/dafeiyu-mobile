package app.dafeiyu.mobile;

public final class ReadinessTest {
    public static void main(String[] args) {
        int count = 0;
        for (boolean phone : new boolean[]{false, true}) {
            for (boolean auth : new boolean[]{false, true}) {
                check(Readiness.evaluate(false, phone, auth, false) != Readiness.State.READY,
                    "A missing engine must never be ready"); count++;
            }
        }
        check(Readiness.evaluate(true, false, true, false) != Readiness.State.READY,
            "An unverified phone must never be ready"); count++;
        check(Readiness.evaluate(true, true, false, false) != Readiness.State.READY,
            "Authentication loss must stop readiness"); count++;
        for (boolean engine : new boolean[]{false, true}) {
            for (boolean phone : new boolean[]{false, true}) {
                for (boolean auth : new boolean[]{false, true}) {
                    check(Readiness.evaluate(engine, phone, auth, true) == Readiness.State.USER_ACTION_REQUIRED,
                        "Challenge or account kick requires user action"); count++;
                }
            }
        }
        check(Readiness.evaluate(true, true, true, false) == Readiness.State.READY,
            "Readiness requires all evidence"); count++;
        System.out.println("readiness checks passed: " + count);
    }

    private static void check(boolean condition, String message) {
        if (!condition) throw new AssertionError(message);
    }
}
