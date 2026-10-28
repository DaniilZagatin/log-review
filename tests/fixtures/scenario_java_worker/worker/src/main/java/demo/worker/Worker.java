package demo.worker;

import java.time.LocalDateTime;

// Synthetic fixture: a queue worker that settles unmatched rent payments.
public class Worker {
    private final TaskQueue queue;
    private final Clients clients;

    public Worker(TaskQueue queue, Clients clients) {
        this.queue = queue;
        this.clients = clients;
    }

    public void work() throws Exception {
        LocalDateTime startTime = LocalDateTime.now();
        Item item;
        while (!queue.isStopRequested()
                && startTime.plusMinutes(15).isAfter(LocalDateTime.now())
                && ((item = queue.nextItem("InWork")) != null)) {
            var dispatcher = new Dispatcher(clients, item);
            try {
                dispatcher.process();
                if (!dispatcher.failed()) {
                    queue.markCompleted(item);
                } else {
                    queue.markException(item, dispatcher.errorMessage());
                }
            } catch (Exception ex) {
                queue.markException(item, ex.getMessage());
            }
        }
    }
}
