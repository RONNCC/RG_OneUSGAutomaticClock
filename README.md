# ⏰ OneUSGAutomaticClock ⏰
## CURRENT STATUS: NOT WORKING ❌
### Page has been updated and CSS selectors need to be updated.
It should only take about 1-2 hours of work to get this working again, but I sadly don't have the time right now.

## What is this?
This is a little script for Georgia Tech students to be able to automatically clock hours without worrying about forgetting to turn it off.

It can be easily modified to work for any university in the USG system 
(If you have any questions on how to do this, feel free to reach out, I can make a fork that works for any of the universities). 

## Requirements (what does the script use?):
- Python 3.12+ [Find Here](https://www.python.org)
- selenium [Find Here](https://www.selenium.dev/documentation/en/)
- chromedriver_autoinstaller [Find Here](https://pypi.org/project/chromedriver-autoinstaller/)
  - [MAC] You probably need to do `brew install --cask chromedriver`

## Set-Up Instructions: 

1. Open a terminal window.
1. Type the following to clone the repo and switch to the project directory
    * `git clone https://github.com/Shaun-Regenbaum/OneUSGAutomaticClock.git` then `cd OneUSGAutomaticClock` 
    * For self-help on how to clone a repo click [here](https://www.howtogeek.com/451360/how-to-clone-a-github-repository/), otherwise reach out and I will help you (seriously I will sit down with you and guide you through it)!
1. Download and install Python from [here](https://www.python.org/downloads/).
    * **Make sure to install Python 3.9 or Later**
1. Once you have python type the following to install the needed dependencies:
    * `pip3 install -r requirements.txt`
1. Finally, run the script from the command line, then sit back and relax (while doing your work):
    * `python3 clock_manager.py -hrs <hours> -u <gt_username>`

**Note: You will be prompted for your GT password**

**Note: You will also need to confirm on duo 2fa (two factor authentication) one time*
## FAQ

### Chromedriver

**Installation**
If you want to get the chromedriver manually, or are having problems, you can download the appropriate chromedriver for your OS from [here](https://sites.google.com/a/chromium.org/chromedriver/home). You will need to manually add this driver to your environment path and and modify the code to correctly reference it [as such](https://chromedriver.chromium.org/getting-started).

**Security**
- **Q: I'm getting "cannot be opened because the developer cannot be verified"**
  
Hit cancel and then go to System Settings -> Privacy & Security -> Scroll down to "Allow applications downloaded from..."  and you should see a "<application> was blocked from use" with a button "Allow Anyway" to click.
![image](https://github.com/RONNCC/OneUSGAutomaticClock/assets/1313566/d0be177c-1985-4d8e-977f-e89c0e6adc5c)


