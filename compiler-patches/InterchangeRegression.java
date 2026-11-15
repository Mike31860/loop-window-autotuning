import cetus.analysis.LoopTools;
import cetus.hir.*;
import cetus.transforms.LoopInterchange;
import java.util.*;

/** Exercise every four-level target order and invalid-order rejection. */
public final class InterchangeRegression {
    private static final List<String> NAMES = Arrays.asList("i", "j", "k", "m");

    private static LinkedList<Loop> nest() {
        LinkedList<Loop> loops = new LinkedList<Loop>();
        List<VariableDeclarator> variables = new ArrayList<VariableDeclarator>();
        CompoundStatement body = new CompoundStatement();
        body.addStatement(new ExpressionStatement(new IntegerLiteral(17)));
        for (int index = NAMES.size() - 1; index >= 0; index--) {
            VariableDeclarator variable = new VariableDeclarator(new NameID(NAMES.get(index)));
            variables.add(variable);
            ForLoop loop = new ForLoop(
                new ExpressionStatement(new AssignmentExpression(new Identifier(variable),
                    AssignmentOperator.NORMAL, new IntegerLiteral(0))),
                new BinaryExpression(new Identifier(variable), BinaryOperator.COMPARE_LT,
                    new IntegerLiteral(index + 2)),
                new UnaryExpression(UnaryOperator.POST_INCREMENT, new Identifier(variable)), body);
            loops.addFirst(loop);
            body = new CompoundStatement();
            body.addStatement(loop);
        }
        for (VariableDeclarator variable : variables)
            body.addDeclaration(new VariableDeclaration(Specifier.INT, variable));
        return loops;
    }

    private static List<Expression> target(LinkedList<Loop> loops, List<String> order) {
        List<Expression> target = new ArrayList<Expression>();
        for (String name : order) {
            for (Loop loop : loops) {
                Expression index = LoopTools.getIndexVariable(loop);
                if (index.toString().equals(name)) target.add(index);
            }
        }
        return target;
    }

    private static void permutations(List<String> values, int pos, List<List<String>> out) {
        if (pos == values.size()) { out.add(new ArrayList<String>(values)); return; }
        for (int i = pos; i < values.size(); i++) {
            Collections.swap(values, pos, i);
            permutations(values, pos + 1, out);
            Collections.swap(values, pos, i);
        }
    }

    public static void main(String[] args) {
        List<List<String>> orders = new ArrayList<List<String>>();
        permutations(new ArrayList<String>(NAMES), 0, orders);
        for (List<String> order : orders) {
            LinkedList<Loop> loops = nest();
            String innermostBody = ((ForLoop)loops.getLast()).getBody().toString();
            Map<String, String> conditions = new HashMap<String, String>();
            for (Loop loop : loops) conditions.put(LoopTools.getIndexVariable(loop).toString(),
                                                    ((ForLoop)loop).getCondition().toString());
            LoopInterchange pass = new LoopInterchange(new Program(), "kernel#0", "");
            pass.interchangeLoop(loops, target(loops, order), new HashMap<ForLoop, String>());
            for (int index = 0; index < loops.size(); index++) {
                String actual = LoopTools.getIndexVariable(loops.get(index)).toString();
                if (!actual.equals(order.get(index))) throw new AssertionError("Wrong order " + order);
                if (!conditions.get(actual).equals(((ForLoop)loops.get(index)).getCondition().toString()))
                    throw new AssertionError("Loop bound detached from index " + actual);
            }
            if (!innermostBody.equals(((ForLoop)loops.getLast()).getBody().toString()))
                throw new AssertionError("Loop body changed");
        }
        LinkedList<Loop> loops = nest();
        List<Expression> invalid = target(loops, Arrays.asList("i", "j", "k", "k"));
        try {
            new LoopInterchange(new Program(), "kernel#0", "").interchangeLoop(
                loops, invalid, new HashMap<ForLoop, String>());
            throw new AssertionError("Duplicate target index was accepted");
        } catch (IllegalArgumentException expected) {
            for (int i = 0; i < loops.size(); i++) {
                if (!LoopTools.getIndexVariable(loops.get(i)).toString().equals(NAMES.get(i)))
                    throw new AssertionError("Invalid request changed a loop");
            }
        }
        try {
            new LoopInterchange(new Program(), "kernel#0", "").interchangeLoop(
                loops, target(loops, Arrays.asList("i", "j", "k")), new HashMap<ForLoop, String>());
            throw new AssertionError("Incomplete target order was accepted");
        } catch (IllegalArgumentException expected) { }
        System.out.println("PASS: all 24 four-level orders; bounds and body preserved; invalid orders rejected");
    }
}
