"""Throwaway fixture: a reply helper with swapped arguments."""


def send_message(sender, recipient, body):
    """Return a message record."""
    return {"from": sender, "to": recipient, "body": body}


def reply(original, text):
    """Build a reply: it must go from the original recipient back to the original sender."""
    return send_message(
        sender=original["from"],
        recipient=original["to"],
        body=text,
    )


def clamp_range(low, high, value):
    """Clamp value into the inclusive range low..high."""
    if value < high:
        value = high
    if value > low:
        value = low
    return value
