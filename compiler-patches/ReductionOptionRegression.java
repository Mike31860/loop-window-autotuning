import cetus.exec.Driver;
import cetus.hir.Program;
import cetus.transforms.ReductionTransform;
import java.lang.reflect.Field;

/** Checks successive loop-pass invocations in the same compiler JVM. */
public final class ReductionOptionRegression {
    public static void main(String[] args) throws Exception {
        Driver.registerOptions();
        String[] requested = {"0", "2", "0", "3", "4", "invalid"};
        int[] expected = {1, 2, 1, 3, 4, 1};
        ReductionTransform[] passes = new ReductionTransform[requested.length];
        Field option = ReductionTransform.class.getDeclaredField("option");
        option.setAccessible(true);
        for (int i = 0; i < requested.length; i++) {
            Driver.setOptionValue("reduction", requested[i]);
            passes[i] = new ReductionTransform(new Program());
            int actual = option.getInt(passes[i]);
            if (actual != expected[i]) {
                throw new AssertionError("Pass " + i + ", reduction=" + requested[i]
                    + ": expected " + expected[i] + ", got " + actual);
            }
        }
        // Constructing a later pass must not change an earlier pass's strategy.
        for (int i = 0; i < passes.length; i++) {
            if (option.getInt(passes[i]) != expected[i]) {
                throw new AssertionError("A later pass changed pass " + i);
            }
        }
        System.out.println("PASS: per-invocation reduction strategies 0,2,0,3,4,invalid");
    }
}
