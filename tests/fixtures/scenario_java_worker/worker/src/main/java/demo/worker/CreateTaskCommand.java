package demo.worker;

public class CreateTaskCommand {
    private final Assignments assignments;
    private final Item item;

    public CreateTaskCommand(Assignments assignments, Item item) {
        this.assignments = assignments;
        this.item = item;
    }

    public void execute() throws Exception {
        assignments.createTask(item.details());
    }
}
