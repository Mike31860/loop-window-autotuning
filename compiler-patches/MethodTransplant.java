import java.nio.file.Files;
import java.nio.file.Paths;
import java.util.List;
import java.util.jar.JarFile;
import jdk.internal.org.objectweb.asm.ClassReader;
import jdk.internal.org.objectweb.asm.ClassWriter;
import jdk.internal.org.objectweb.asm.tree.ClassNode;
import jdk.internal.org.objectweb.asm.tree.MethodNode;
import jdk.internal.org.objectweb.asm.util.Textifier;
import jdk.internal.org.objectweb.asm.util.TraceMethodVisitor;

/** Replace one method while retaining the deployed class's other bytecode. */
public final class MethodTransplant {
    private static ClassNode read(byte[] bytes) {
        ClassNode node = new ClassNode();
        new ClassReader(bytes).accept(node, 0);
        return node;
    }

    private static MethodNode find(ClassNode node, String name, String descriptor) {
        for (Object value : node.methods) {
            MethodNode method = (MethodNode) value;
            if (method.name.equals(name) && method.desc.equals(descriptor)) return method;
        }
        throw new IllegalArgumentException("Missing method " + name + descriptor);
    }

    private static String trace(MethodNode method) {
        Textifier text = new Textifier();
        method.accept(new TraceMethodVisitor(text));
        return text.getText().toString();
    }

    public static void main(String[] args) throws Exception {
        if (args.length != 6) throw new IllegalArgumentException(
            "original.jar entry compiled.class output.class method descriptor");
        byte[] original;
        try (JarFile jar = new JarFile(args[0])) {
            try (java.io.InputStream input = jar.getInputStream(jar.getJarEntry(args[1]))) {
                java.io.ByteArrayOutputStream output = new java.io.ByteArrayOutputStream();
                byte[] buffer = new byte[8192];
                int count;
                while ((count = input.read(buffer)) != -1) output.write(buffer, 0, count);
                original = output.toByteArray();
            }
        }
        ClassNode before = read(original);
        ClassNode output = read(original);
        ClassNode compiled = read(Files.readAllBytes(Paths.get(args[2])));
        if (!before.name.equals(compiled.name)) throw new IllegalArgumentException("Class mismatch");
        MethodNode previous = find(output, args[4], args[5]);
        MethodNode replacement = find(compiled, args[4], args[5]);
        output.methods.set(output.methods.indexOf(previous), replacement);
        ClassWriter writer = new ClassWriter(0);
        output.accept(writer);
        byte[] bytes = writer.toByteArray();
        ClassNode after = read(bytes);
        int preserved = 0;
        for (Object value : before.methods) {
            MethodNode method = (MethodNode) value;
            if (method.name.equals(args[4]) && method.desc.equals(args[5])) continue;
            if (!trace(method).equals(trace(find(after, method.name, method.desc)))) {
                throw new AssertionError("Unrelated method changed: " + method.name + method.desc);
            }
            preserved++;
        }
        Files.createDirectories(Paths.get(args[3]).getParent());
        Files.write(Paths.get(args[3]), bytes);
        System.out.println("PASS: replaced " + args[1] + "::" + args[4] + args[5]
            + "; preserved " + preserved + " other methods including their code and debug metadata");
    }
}
