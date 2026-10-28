package demo.worker;

public class CloseTaskCommand {
    private final TaskBoard board;
    private final Item item;
    private final String comment;

    public CloseTaskCommand(TaskBoard board, Item item, String comment) {
        this.board = board;
        this.item = item;
        this.comment = comment;
    }

    public void execute() throws Exception {
        board.closeTask(item.id(), comment);
    }
}
