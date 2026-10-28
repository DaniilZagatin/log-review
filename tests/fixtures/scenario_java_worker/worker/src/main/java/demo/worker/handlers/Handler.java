package demo.worker.handlers;

import demo.worker.Item;

public interface Handler {
    void handle(Item item, String id) throws Exception;
}
