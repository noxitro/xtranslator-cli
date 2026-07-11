"""Message-based access to xTranslator's native main menu.

xTranslator 1.6+ draws its menu items owner-drawn, so their captions cannot be
read with GetMenuItemInfo, and this may run in a session without an interactive
foreground window, so real mouse/keyboard input is not an option either.

What does work without focus:
  - MSAA (oleacc OBJID_MENU) exposes the *top-level* item names ("File" ...
    "Wizards") because VCL implements menu accessibility.
  - WM_INITMENUPOPUP makes VCL build a lazily-created submenu's HMENU.
  - GetMenuItemID / GetMenuState work on owner-drawn items.
  - Posting WM_COMMAND with the item id executes the menu handler.

Submenu item captions stay unreadable, so the caller fires candidate item ids
(enabled, non-separator) and verifies which one opened the expected dialog.
"""

from __future__ import annotations

import ctypes
from ctypes import byref, windll

WM_COMMAND = 0x0111
WM_INITMENUPOPUP = 0x0117
MF_BYPOSITION = 0x0400
MF_SEPARATOR = 0x0800
MF_GRAYED = 0x0001
MF_DISABLED = 0x0002

_user32 = windll.user32

_IAccessible = None


def _iaccessible():
    global _IAccessible
    if _IAccessible is None:
        import comtypes.client
        comtypes.client.GetModule("oleacc.dll")
        from comtypes.gen.Accessibility import IAccessible
        _IAccessible = IAccessible
    return _IAccessible


def top_level_menu_names(hwnd: int) -> list[str]:
    """Read the main menu bar item captions via MSAA."""
    IAccessible = _iaccessible()
    import comtypes.automation as automation

    OBJID_MENU = ctypes.c_long(-3)
    menubar = ctypes.POINTER(IAccessible)()
    windll.oleacc.AccessibleObjectFromWindow(
        hwnd, OBJID_MENU, byref(IAccessible._iid_), byref(menubar))
    count = menubar.accChildCount
    variants = (automation.VARIANT * count)()
    fetched = ctypes.c_long()
    windll.oleacc.AccessibleChildren(menubar, 0, count, variants, byref(fetched))
    names = []
    for i in range(fetched.value):
        try:
            acc = variants[i].value.QueryInterface(IAccessible)
            names.append(acc.accName(0) or "")
        except Exception:
            names.append("")
    return names


def submenu_command_ids(hwnd: int, top_index: int) -> list[int]:
    """Item ids of one top-level submenu (enabled, non-separator), in menu order.

    Sends WM_INITMENUPOPUP first so VCL populates the lazily-built HMENU.
    """
    hmenu = _user32.GetMenu(hwnd)
    hsub = _user32.GetSubMenu(hmenu, top_index)
    if not hsub:
        return []
    _user32.SendMessageW(hwnd, WM_INITMENUPOPUP, hsub, top_index)
    ids = []
    for pos in range(_user32.GetMenuItemCount(hsub)):
        state = _user32.GetMenuState(hsub, pos, MF_BYPOSITION)
        if state == 0xFFFFFFFF or state & (MF_SEPARATOR | MF_GRAYED | MF_DISABLED):
            continue
        mid = _user32.GetMenuItemID(hsub, pos)
        if mid not in (0, 0xFFFFFFFF):
            ids.append(mid)
    return ids


def post_command(hwnd: int, command_id: int) -> None:
    _user32.PostMessageW(hwnd, WM_COMMAND, command_id, 0)
