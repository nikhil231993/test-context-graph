package org.example.library.model;

import java.time.Clock;

public class LibraryCard {

    private final String number;

    private final Issuer issuer;

    private LibraryCard(Builder builder) {
        this.number = builder.number;
        this.issuer = builder.issuer;
    }

    public static Builder builder() {
        return new Builder();
    }

    /** Builds cards. */
    public static class Builder {

        private String number;

        private Issuer issuer;

        public Builder number(String number) {
            this.number = number;
            return this;
        }

        public LibraryCard build() {
            this.validate();
            issuer.register(number);
            return new LibraryCard(this);
        }

        private void validate() {
        }
    }

    class Stamp {

        private final Clock clock = Clock.systemUTC();

        String stamp() {
            return issuer.sign(clock.instant());
        }

        enum Color { RED, BLUE }
    }
}
