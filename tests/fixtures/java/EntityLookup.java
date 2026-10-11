package org.example.library.util;

import java.util.Collection;

import org.example.library.model.BaseItem;

/**
 * Helpers to find items in collections.
 */
public abstract class EntityLookup {

    /**
     * The item with the given id.
     *
     * @throws ItemNotFoundException if there is none
     */
    public static <T extends BaseItem> T getById(Collection<T> items, Class<T> itemClass, int itemId)
        throws ItemNotFoundException {
        for (T item : items) {
            if (item.getId() == itemId && itemClass.isInstance(item)) {
                return item;
            }
        }
        throw new ItemNotFoundException(itemClass, itemId);
    }

    public static String describe(Object... parts) {
        return String.join(", ", java.util.Arrays.stream(parts).map(String::valueOf).toList());
    }
}
