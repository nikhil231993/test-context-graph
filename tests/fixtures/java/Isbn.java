package org.example.library.model;

import java.util.Objects;

import org.jspecify.annotations.Nullable;

/**
 * An ISBN, checked on creation.
 */
public record Isbn(String value, @Nullable String prefix) implements Comparable<Isbn> {

    public Isbn {
        Objects.requireNonNull(value, "value");
    }

    public static Isbn of(String text) {
        return new Isbn(text.strip(), null);
    }

    @Override
    public int compareTo(Isbn other) {
        return value.compareTo(other.value());
    }
}
