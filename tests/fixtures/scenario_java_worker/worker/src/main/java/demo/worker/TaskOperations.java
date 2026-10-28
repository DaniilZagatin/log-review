package demo.worker;

public class TaskOperations {
    private final Clients clients;

    public TaskOperations(Clients clients) {
        this.clients = clients;
    }

    public void rejectAndClose(Item item, String documentId, String reason) throws Exception {
        new RejectCommand(clients.ledger(), documentId, reason).execute();
        new CloseTaskCommand(clients.board(), item, "Payment rejected").execute();
    }

    public void resubmitAndClose(Item item, String contractId) throws Exception {
        new ResubmitCommand(clients.ledger(), item.documentId(), contractId).execute();
        new CloseTaskCommand(clients.board(), item, "Payment settled").execute();
    }

    public void closeAndCreate(Item item) throws Exception {
        new CloseTaskCommand(clients.board(), item, "Manual task created").execute();
        new CreateTaskCommand(clients.assignments(), item).execute();
    }
}
