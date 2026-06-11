> Archived from the community-maintained Picasa Resources site:
> <https://sites.google.com/site/picasaresources/picasa/windows-network-share>
> Retrieved 2026-06-11 (text extracted with trafilatura).

The following info was suggested by user Matt Wilkie in https://support.google.com/photos/thread/151552592.  

You can post comments there.

"I wanted Picasa to index a photo collection on a Windows network share, but the Folder Manager dialog only shows local devices and mapped network drives. I don't have a reliable drive letter to use so wanted to use the network share directly using Universal Name Convention (UNC) path. After some trial and error I discovered it's actually quite easy, but not from within Picasa.

1. Close Picasa

2. Edit your 'watchedfolders.txt'. (see How Picasa works)

3. Add "\\server\share\path\to\photos\"

4. Launch Picasa

The trailing backslash is critical, "\\server\share\path\to\photos" does nothing. There is no error message.
