"""Explicit synthetic counterpart of the blocking account-update SDK contract."""

from eventkit import Event


class SyntheticAccountRefresh:
    @property
    def accountValueEvent(self):
        if "_synthetic_account_value_event" not in self.__dict__:
            self._synthetic_account_value_event = Event()
        return self._synthetic_account_value_event

    def reqAccountUpdates(self, account):
        # Each call emits this fixture's latest values before completing.
        # A cached row alone is deliberately insufficient in the executor.
        for row in self.accountValues() or ():
            if getattr(row, "account", "") == account:
                self.accountValueEvent.emit(row)


def with_synthetic_account_refresh(ib):
    ib.accountValueEvent = Event()
    ib.reqAccountUpdates = lambda account: [
        ib.accountValueEvent.emit(row) for row in ib.accountValues() or ()
        if getattr(row, "account", "") == account
    ]
    return ib
