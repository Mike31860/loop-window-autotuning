import cetus.exec.Driver;
import cetus.hir.Program;
import cetus.transforms.IVSubstitution;
import java.lang.reflect.Field;

/** The targeted constructor must honor the same mode as the normal constructor. */
public final class InductionOptionRegression {
    public static void main(String[] args) throws Exception {
        Driver.registerOptions();
        Field option = IVSubstitution.class.getDeclaredField("option");
        option.setAccessible(true);
        String[] modes = {"3", "1", "2", "invalid", "0"};
        for (String mode : modes) {
            Driver.setOptionValue("induction", mode);
            IVSubstitution normal = new IVSubstitution(new Program());
            IVSubstitution targeted = new IVSubstitution(new Program(), "kernel#0", "");
            int expected = option.getInt(normal);
            int actual = option.getInt(targeted);
            if (actual != expected) throw new AssertionError(
                "induction=" + mode + ": expected " + expected + ", got " + actual);
        }
        System.out.println("PASS: targeted induction modes match the normal constructor");
    }
}
