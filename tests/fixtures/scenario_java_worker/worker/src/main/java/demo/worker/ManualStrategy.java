package demo.worker;

public class ManualStrategy implements Strategy {
    private final Clients clients;

    public ManualStrategy(Clients clients) {
        this.clients = clients;
    }

    @Override
    public void process(Item item) throws Exception {
        item.setDetails(clients.board().taskInfo(item.id()));
        new CloseTaskCommand(clients.board(), item, "Payment not matched").execute();
        new CreateTaskCommand(clients.assignments(), item).execute();
    }
}
