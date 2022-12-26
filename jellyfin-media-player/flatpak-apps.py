#!/usr/bin/python3
"""Menu popup for selecting a flatpak app to run."""
# FIXME: Doesn't support joystick input
import gi
import pathlib

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk  # noqa: E402 "module level import not at top of file"
from gi.repository import Gio  # noqa: E402 "module level import not at top of file"

APPLICATIONS_PATH = pathlib.Path('/var/lib/flatpak/exports/share/applications/')


class ButtonWindow(Gtk.Window):
    """GTK Window."""

    def __init__(self):
        """Create the window, and add a button to it for each exported flatpak .desktop file."""
        super().__init__(title="Flatpak app selector")

        flowbox = Gtk.FlowBox(max_children_per_line=8)
        self.add(flowbox)

        for app_path in APPLICATIONS_PATH.glob('*.desktop'):
            app = Gio.DesktopAppInfo.new_from_filename(str(app_path))
            print(app.get_name(), app.get_commandline(), sep=': ')

            button = Gtk.Button(label=app.get_name(),
                                image=Gtk.Image.new_from_gicon(app.get_icon(), Gtk.IconSize.DIALOG),
                                always_show_image=True,
                                image_position=Gtk.PositionType.TOP)

            # This is just a random attribute so that on_pressed can find this info,
            # but I really want to make sure it doesn't conflict with any of the Gtk attribute names.
            button.PYTHON_app_data = app

            button.connect("clicked", self.on_pressed)

            # Because FlowBoxes are a bit weird, they'll work fine with mouse,
            # but the 'clicked' event doesn't make it through to the button when using keyboard navigation.
            # So instead of just relying on the add() function to automatically sort out the FlowBoxChild,
            # if I explicitly create that child then I can add my own 'activate' signal handler on that,
            # which does work with keyboard navigation.
            flowboxchild = Gtk.FlowBoxChild()
            flowboxchild.add(button)
            flowboxchild.connect("activate", lambda _, app_button=button: self.on_pressed(app_button))
            flowbox.add(flowboxchild)

    def on_pressed(self, button):
        """Button pressed, launch app."""
        button.PYTHON_app_data.launch()
        Gtk.main_quit()


if len(list(APPLICATIONS_PATH.glob('*.desktop'))) == 1:
    # Don't bother with the popup menu if we only have one app anyway
    app_path = list(APPLICATIONS_PATH.glob('*.desktop'))[0]
    app = Gio.DesktopAppInfo.new_from_filename(str(app_path))
    print(app.get_name(), app.get_commandline(), sep=': ')
    app.launch()
else:
    win = ButtonWindow()
    win.connect("destroy", Gtk.main_quit)
    win.show_all()
    Gtk.main()
