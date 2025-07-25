import sys
import webview


def main():
    if len(sys.argv) < 2:
        print("Usage: webview_embed.py <url> [width height x y]")
        return
    url = sys.argv[1]
    width = int(sys.argv[2]) if len(sys.argv) > 2 else 800
    height = int(sys.argv[3]) if len(sys.argv) > 3 else 600
    x = int(sys.argv[4]) if len(sys.argv) > 4 else None
    y = int(sys.argv[5]) if len(sys.argv) > 5 else None

    window = webview.create_window(
        "",
        url,
        width=width,
        height=height,
        x=x,
        y=y,
        resizable=False,
        frameless=True,
        on_top=True,
    )
    gui = "edgechromium" if sys.platform.startswith("win") else None
    webview.start(gui=gui)


if __name__ == "__main__":
    main()
