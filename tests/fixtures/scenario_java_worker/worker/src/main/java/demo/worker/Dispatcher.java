package demo.worker;

public class Dispatcher {
    private static final String NOT_MATCHED = "Payment not matched";
    private static final String TOO_FEW_DETAILS = "Too few details to match";

    private final Clients clients;
    private final Item item;
    private boolean failed;
    private String errorMessage;

    public Dispatcher(Clients clients, Item item) {
        this.clients = clients;
        this.item = item;
    }

    public void process() throws Exception {
        clients.board().takeToWork(item.id());
        try {
            if (NOT_MATCHED.equals(item.error()) || TOO_FEW_DETAILS.equals(item.error())) {
                Strategy strategy = isSupportedKind(item.paymentKind())
                        ? new MatchingStrategy(clients)
                        : new ManualStrategy(clients);
                strategy.process(item);
            } else {
                new ManualStrategy(clients).process(item);
            }
        } catch (Exception exc) {
            failed = true;
            errorMessage = exc.getMessage();
            clients.assignments().createTask(clients.board().taskInfo(item.id()));
            clients.board().closeTask(item.id(), "Manual task created");
        }
    }

    public boolean failed() {
        return failed;
    }

    public String errorMessage() {
        return errorMessage;
    }

    private boolean isSupportedKind(String kind) {
        return "Rent".equalsIgnoreCase(kind) || "Deposit".equalsIgnoreCase(kind) || "Standard".equalsIgnoreCase(kind);
    }
}
